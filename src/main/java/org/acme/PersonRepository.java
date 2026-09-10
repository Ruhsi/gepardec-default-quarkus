package org.acme;

import io.quarkus.hibernate.orm.panache.PanacheRepository;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.persistence.EntityManager;
import jakarta.persistence.LockModeType;
import jakarta.persistence.PersistenceContext;
import org.hibernate.Criteria;
import org.hibernate.Session;
import org.hibernate.criterion.Order;
import org.hibernate.criterion.Restrictions;

import java.util.List;

@ApplicationScoped
public class PersonRepository implements PanacheRepository<Person> {

    @PersistenceContext
    EntityManager entityManager;

    public Person persistPerson(Person person) {
        entityManager.persist(person);
        return person;
    }

    public Person mergePerson(Person person) {
        return entityManager.merge(person);
    }

    public void flushEntityManager() {
        entityManager.flush();
    }

    public void refreshPerson(Person person) {
        entityManager.refresh(person);
    }

    public Person findByIdWithLock(Long id) {
        return entityManager.find(
                Person.class,
                id,
                LockModeType.PESSIMISTIC_WRITE
        );
    }

    public List<Person> findByLastNameWithJpql(String lastName) {
        return entityManager
                .createQuery(
                        "select p from Person p where p.lastName = :lastName",
                        Person.class
                )
                .setParameter("lastName", lastName)
                .getResultList();
    }

    @SuppressWarnings("unchecked")
    public List<Person> findByLastNameWithNativeQuery(String lastName) {
        return entityManager
                .createNativeQuery(
                        "select * from person where last_name = :lastName",
                        Person.class
                )
                .setParameter("lastName", lastName)
                .getResultList();
    }

    public Session unwrapHibernateSession() {
        return entityManager.unwrap(Session.class);
    }

    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findUsingLegacyCriteria(String lastName) {
        Session session = entityManager.unwrap(Session.class);

        Criteria criteria = session.createCriteria(Person.class);
        criteria.add(Restrictions.eq("lastName", lastName));

        return criteria.list();
    }

    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findLatestUsingLegacyCriteria() {
        Session session = entityManager.unwrap(Session.class);

        Criteria criteria = session.createCriteria(Person.class);
        criteria.add(Restrictions.eq("active", true));
        criteria.addOrder(Order.desc("createdAt"));
        criteria.setMaxResults(10);

        return criteria.list();
    }

    public List<Person> findWithPanache(String lastName) {
        return find("lastName", lastName).list();
    }

    public Person findByIdWithPanache(Long id) {
        return findById(id);
    }
}