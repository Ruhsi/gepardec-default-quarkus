package org.acme;

import io.quarkus.hibernate.orm.panache.PanacheRepository;
import jakarta.persistence.EntityManager;
import jakarta.transaction.Transactional;
import org.hibernate.LockMode;
import org.hibernate.LockOptions;
import org.hibernate.Session;
import org.hibernate.query.Query;
import org.hibernate.type.StandardBasicTypes;

import javax.enterprise.context.ApplicationScoped;
import javax.inject.Inject;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Optional;

@ApplicationScoped
public class PersonRepository implements PanacheRepository<Person> {

    @Inject
    EntityManager entityManager;

    /**
     * Bridge from JPA to the Hibernate 5 Session API.
     */
    public Session getHibernateSession() {
        return entityManager.unwrap(Session.class);
    }

    private Session session() {
        return getHibernateSession();
    }

    /**
     * Hibernate 5 classic Criteria API.
     * In Hibernate 6, Session#createCriteria and org.hibernate.Criteria are gone.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findByLastNameUsingClassicCriteria(String lastName) {
        Query<Person> query = session().createQuery(
                "from Person p where p.lastName = :lastName order by p.firstName asc",
                Person.class
        );
        return query
                .setParameter("lastName", lastName)
                .list();
    }

    /**
     * Old Hibernate 5 uniqueResult pattern.
     */
    @SuppressWarnings("deprecation")
    public Optional<Person> findByEmailUsingClassicCriteria(String email) {
        Query<Person> query = session().createQuery(
                "from Person p where p.email = :email",
                Person.class
        );
        return query
                .setParameter("email", email)
                .setMaxResults(1)
                .list()
                .stream()
                .findFirst();
    }

    /**
     * Criteria + MatchMode/ilike is another legacy Hibernate 5 pattern.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findActiveByCityIgnoringCase(String city) {
        Query<Person> query = session().createQuery(
                "from Person p where p.active = true and lower(p.city) = lower(:city) order by p.createdAt desc",
                Person.class
        );
        return query
                .setParameter("city", city)
                .list();
    }

    /**
     * Example queries are Hibernate-5 specific and were removed from Hibernate 6.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findByExampleUsingHibernateExample(Person probe) {
        StringBuilder hql = new StringBuilder("from Person p where 1=1");
        java.util.Map<String, Object> parameters = new java.util.LinkedHashMap<>();

        try {
            for (java.beans.PropertyDescriptor propertyDescriptor : java.beans.Introspector.getBeanInfo(Person.class, Object.class).getPropertyDescriptors()) {
                String property = propertyDescriptor.getName();
                if ("id".equals(property) || "version".equals(property) || "createdAt".equals(property) || "active".equals(property)) {
                    continue;
                }

                java.lang.reflect.Method readMethod = propertyDescriptor.getReadMethod();
                if (readMethod == null) {
                    continue;
                }

                Object value = readMethod.invoke(probe);
                if (value == null) {
                    continue;
                }

                if (value instanceof Number && ((Number) value).doubleValue() == 0.0d) {
                    continue;
                }

                if (value instanceof CharSequence && ((CharSequence) value).length() == 0) {
                    continue;
                }

                if (value instanceof String) {
                    String parameterName = property + "Prefix";
                    hql.append(" and lower(p.").append(property).append(") like :").append(parameterName);
                    parameters.put(parameterName, ((String) value).toLowerCase() + "%");
                } else {
                    hql.append(" and p.").append(property).append(" = :").append(property);
                    parameters.put(property, value);
                }
            }
        } catch (ReflectiveOperationException | java.beans.IntrospectionException ex) {
            throw new IllegalStateException("Failed to build example query", ex);
        }

        hql.append(" order by p.lastName asc, p.firstName asc");

        Query<Person> query = session().createQuery(hql.toString(), Person.class);
        for (java.util.Map.Entry<String, Object> entry : parameters.entrySet()) {
            query.setParameter(entry.getKey(), entry.getValue());
        }

        return query.list();
    }

    /**
     * Legacy Criteria with order + limit.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findLatestActiveUsingClassicCriteria(int maxResults) {
        Query<Person> query = session().createQuery(
                "from Person p where p.active = true order by p.createdAt desc",
                Person.class
        );
        return query
                .setMaxResults(maxResults)
                .list();
    }

    /**
     * Projection API in the old Hibernate 5 Criteria style.
     */
    @SuppressWarnings("deprecation")
    public long countByLastNameUsingProjection(String lastName) {
        Long result = session().createQuery(
                "select count(p) from Person p where p.lastName = :lastName",
                Long.class
        )
        .setParameter("lastName", lastName)
                .uniqueResult();
        return result;
    }

    /**
     * Projection + ResultTransformer is a classic Hibernate 5 migration hotspot.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Map<String, Object>> findProjectedActivePeople() {
        Query<?> query = session().createQuery(
                "select new map(" +
                        "p.id as id, " +
                        "p.firstName as firstName, " +
                        "p.lastName as lastName, " +
                        "p.email as email, " +
                        "p.createdAt as createdAt" +
                        ") from Person p where p.active = true"
        );
        return (List<Map<String, Object>>) (List<?>) query.list();
    }

    /**
     * SQLRestriction is another old Hibernate-only feature.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findByEmailDomainUsingSqlRestriction(String emailDomain) {
        Query<Person> query = session().createQuery(
                "from Person p where lower(p.email) like :pattern order by p.email asc",
                Person.class
        );
        return query
                .setParameter("pattern", "%@" + emailDomain.toLowerCase())
                .list();
    }

    /**
     * DetachedCriteria + Subqueries are strong examples of Hibernate-5 specific query code.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findPeopleCreatedAtLatestTimestamp() {
        Query<Person> query = session().createQuery(
                "from Person p where p.createdAt = (select max(p2.createdAt) from Person p2)",
                Person.class
        );
        return query.list();
    }

    /**
     * HQL over the native Hibernate Session.
     */
    public List<Person> findCreatedAfterUsingHibernateQuery(Instant createdAfter) {
        Query<Person> query = session().createQuery(
                "from Person p where p.createdAt >= :createdAfter order by p.createdAt desc",
                Person.class
        );

        return query
                .setParameter("createdAfter", createdAfter)
                .list();
    }

    /**
     * Explicit locking via Hibernate Session/LockOptions.
     */
    @Transactional
    public Person findAndLockHibernate(Long id) {
        Person person = session().get(Person.class, id);

        if (person != null) {
            session().buildLockRequest(new LockOptions(LockMode.PESSIMISTIC_WRITE)).lock(person);
        }

        return person;
    }

    /**
     * saveOrUpdate is intentionally Hibernate-centric.
     */
    @Transactional
    public Person saveWithHibernateSession(Person person) {
        session().saveOrUpdate(person);
        session().flush();
        return person;
    }

    /**
     * Hibernate HQL bulk update.
     */
    @Transactional
    public int deactivateByCityUsingBulkHql(String city) {
        Query<?> query = session().createQuery(
                "update Person p set p.active = false where p.city = :city"
        );

        return query
                .setParameter("city", city)
                .executeUpdate();
    }

    /**
     * Hibernate HQL bulk delete.
     */
    @Transactional
    public int deleteInactiveUsingBulkHql() {
        Query<?> query = session().createQuery(
                "delete from Person p where p.active = false"
        );

        return query.executeUpdate();
    }
}