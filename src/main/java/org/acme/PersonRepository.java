package org.acme;

import io.quarkus.hibernate.orm.panache.PanacheRepository;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import jakarta.persistence.EntityManager;
import jakarta.persistence.Tuple;
import jakarta.persistence.criteria.CriteriaBuilder;
import jakarta.persistence.criteria.CriteriaQuery;
import jakarta.persistence.criteria.Predicate;
import jakarta.persistence.criteria.Root;
import jakarta.transaction.Transactional;
import org.hibernate.LockMode;
import org.hibernate.LockOptions;
import org.hibernate.Session;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;

@ApplicationScoped
public class PersonRepository implements PanacheRepository<Person> {

    @Inject
    EntityManager entityManager;

    public Session getHibernateSession() {
        return entityManager.unwrap(Session.class);
    }

    private Session session() {
        return getHibernateSession();
    }

    public List<Person> findByLastNameUsingClassicCriteria(String lastName) {
        return session().createSelectionQuery(
                "from Person where lastName = :lastName order by firstName", Person.class)
                .setParameter("lastName", lastName)
                .getResultList();
    }

    public Optional<Person> findByEmailUsingClassicCriteria(String email) {
        return session().createSelectionQuery("from Person where email = :email", Person.class)
                .setParameter("email", email)
                .setMaxResults(1)
                .uniqueResultOptional();
    }

    public List<Person> findActiveByCityIgnoringCase(String city) {
        return session().createSelectionQuery(
                "from Person where active = true and city ilike :city order by createdAt desc", Person.class)
                .setParameter("city", city)
                .getResultList();
    }

    /**
     * Matches non-null string properties by case-insensitive prefix, as Hibernate Example did.
     */
    public List<Person> findByExampleUsingHibernateExample(Person probe) {
        CriteriaBuilder builder = entityManager.getCriteriaBuilder();
        CriteriaQuery<Person> query = builder.createQuery(Person.class);
        Root<Person> person = query.from(Person.class);
        List<Predicate> predicates = new ArrayList<>();
        addPrefixPredicate(builder, person, predicates, "firstName", probe.firstName);
        addPrefixPredicate(builder, person, predicates, "lastName", probe.lastName);
        addPrefixPredicate(builder, person, predicates, "email", probe.email);
        addPrefixPredicate(builder, person, predicates, "city", probe.city);
        query.select(person).where(predicates.toArray(new Predicate[0]))
                .orderBy(builder.asc(person.get("lastName")), builder.asc(person.get("firstName")));
        return entityManager.createQuery(query).getResultList();
    }

    private void addPrefixPredicate(CriteriaBuilder builder, Root<Person> person,
                                    List<Predicate> predicates, String property, String value) {
        if (value != null) {
            predicates.add(builder.like(builder.lower(person.get(property)), value.toLowerCase(Locale.ROOT) + "%"));
        }
    }

    public List<Person> findLatestActiveUsingClassicCriteria(int maxResults) {
        return session().createSelectionQuery(
                "from Person where active = true order by createdAt desc", Person.class)
                .setMaxResults(maxResults)
                .getResultList();
    }

    public long countByLastNameUsingProjection(String lastName) {
        return session().createSelectionQuery("select count(*) from Person where lastName = :lastName", Long.class)
                .setParameter("lastName", lastName)
                .getSingleResult();
    }

    public List<Map<String, Object>> findProjectedActivePeople() {
        List<Tuple> rows = session().createSelectionQuery(
                "select id as id, firstName as firstName, lastName as lastName, email as email, "
                        + "createdAt as createdAt from Person where active = true", Tuple.class)
                .getResultList();
        return rows.stream().map(row -> {
            Map<String, Object> values = new LinkedHashMap<>();
            row.getElements().forEach(element -> values.put(element.getAlias(), row.get(element.getAlias())));
            return values;
        }).toList();
    }

    public List<Person> findByEmailDomainUsingSqlRestriction(String emailDomain) {
        return session().createSelectionQuery(
                "from Person where lower(email) like :domain order by email", Person.class)
                .setParameter("domain", "%@" + emailDomain.toLowerCase(Locale.ROOT))
                .getResultList();
    }

    public List<Person> findPeopleCreatedAtLatestTimestamp() {
        return session().createSelectionQuery(
                "from Person where createdAt = (select max(p.createdAt) from Person p)", Person.class)
                .getResultList();
    }

    public List<Person> findCreatedAfterUsingHibernateQuery(Instant createdAfter) {
        return session().createSelectionQuery(
                "from Person p where p.createdAt >= :createdAfter order by p.createdAt desc", Person.class)
                .setParameter("createdAfter", createdAfter)
                .getResultList();
    }

    @Transactional
    public Person findAndLockHibernate(Long id) {
        Person person = session().get(Person.class, id);
        if (person != null) {
            session().buildLockRequest(new LockOptions(LockMode.PESSIMISTIC_WRITE)).lock(person);
        }
        return person;
    }

    @Transactional
    public Person saveWithHibernateSession(Person person) {
        Person saved;
        if (person.id == null) {
            session().persist(person);
            saved = person;
        } else {
            saved = session().merge(person);
        }
        session().flush();
        return saved;
    }

    @Transactional
    public int deactivateByCityUsingBulkHql(String city) {
        return session().createMutationQuery("update Person p set p.active = false where p.city = :city")
                .setParameter("city", city)
                .executeUpdate();
    }

    @Transactional
    public int deleteInactiveUsingBulkHql() {
        return session().createMutationQuery("delete from Person p where p.active = false")
                .executeUpdate();
    }
}
